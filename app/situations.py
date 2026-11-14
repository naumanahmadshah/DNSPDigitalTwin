"""Situations: the detail behind each type (what is happening, what it affects), the plan (what we recommend, what happens if we
act) and the write-back when a person decides."""
import datetime as dt
import json
import math
import threading
import uuid
from zoneinfo import ZoneInfo

import core
import overdue as OD
from core import pg, pq
from rm import data as RD, dispatch as RX, plan as RP, store as RS

TYPE_LABEL = {"storm": "Storm", "fire": "Fire", "trip": "Feeder trip", "heat": "Hot transformer", "min_load": "Minimum load",
              "vegetation": "Vegetation", "drone_defect": "Drone defect", "fatigue": "Crew fatigue", "records": "Records disagree",
              "overdue": "Crew overdue"}


# The five stages a situation goes through, in order: its label on the list and what it means (the chip's hover note shows which of the five)
STAGES = [("detected", "Detected", "The detector raised it. No one has opened it yet, so no actions are recommended."),
          ("plan_ready", "Plan ready", "The recommended actions have been worked out and are awaiting the duty manager. No action has been taken yet."),
          ("decided", "Partly decided", "Some recommendations have been accepted, adjusted or rejected. The others are still awaiting a decision."),
          ("executing", "Actions under way", "Every recommendation has been decided, and the accepted actions (crews, notices, switching) are being carried out."),
          ("closed", "Closed", "Resolved. The situation leaves the list.")]
STAGE = {code: (i + 1, label, meaning) for i, (code, label, meaning) in enumerate(STAGES)}


def _qty(n, one, many=None):
    """A count with its noun, singular for one: '1 crew', '2 crews', '1,268 customers'."""
    n = round(n)
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _still_off(n):
    """'1 customer is still off' or '1,268 customers are still off'."""
    n = round(n)
    return f"{n:,} customer{'' if n == 1 else 's'} {'is' if n == 1 else 'are'} still off"


def stage_note(status):
    """The hover note of a situation's status chip: which of the five stages it is in, what that means, and the whole path."""
    n, label, meaning = STAGE.get(status, (0, status, ""))
    path = "  →  ".join(f"{i + 1} {lab}" for i, (_, lab, _) in enumerate(STAGES))
    return f"Stage {n} of {len(STAGES)}: {label}\n{'●' * n}{'○' * (len(STAGES) - n)}\n{meaning}\n\n{path}"


class NotFound(LookupError):
    """An unknown situation or recommendation id (the API answers 404)."""


def _sit(sid):
    s = pg.q("SELECT * FROM atlas.situation WHERE situation_id = %s", (sid,), one=True)
    if not s:
        raise NotFound(sid)
    return s


# Revenue at risk if a situation plays out: the reliability incentive the network stands to lose on the customer minutes off supply,
# plus the network revenue on the energy it cannot deliver. Illustrative rates (a real distributor's come from its AER determination);
# a trip uses its live restoration estimate, other situations a typical duration for their kind.
RISK = dict(incentive_per_customer_minute=2.10, tariff_per_kwh=0.14, kw_per_customer=1.3,
            hours={"trip": 3, "storm": 6, "fire": 8, "heat": 6, "vegetation": 4, "drone_defect": 3})


def _at_risk(r):
    hours = RISK["hours"].get(r["type"], 0)
    customers = int(r.get("customers") or 0)
    if r["type"] == "trip" and r.get("customers_off") is not None:
        customers = int(r["customers_off"] or 0)
        if r.get("etr"):
            etr = r["etr"] if isinstance(r["etr"], dt.datetime) else dt.datetime.fromisoformat(str(r["etr"]).replace("Z", "+00:00"))
            if etr.tzinfo is None:
                etr = etr.replace(tzinfo=dt.timezone.utc)
            left = (etr - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
            hours = max(0.5, min(24, left)) if left > 0 else 1
    value = customers * hours * (60 * RISK["incentive_per_customer_minute"] + RISK["kw_per_customer"] * RISK["tariff_per_kwh"])
    return customers, round(hours, 1), round(value)


def risk_basis(r):
    """How a situation's revenue at risk is estimated, in words with its own numbers (the hover note on the list)."""
    if not r["revenue_at_risk"]:
        return "No revenue is at risk: this situation takes no customers off supply."
    unit = "hour" if r["risk_hours"] == 1 else "hours"
    why = (f"{unit} left on the live restoration estimate" if r["type"] == "trip" and r.get("etr") else
           f"{unit} off supply, the typical length for this kind of situation")
    return (f"Estimate: {_qty(r['customers_impacted'], 'customer')} × {r['risk_hours']:g} {why}. Each customer hour costs "
            f"${60 * RISK['incentive_per_customer_minute']:,.0f} in reliability incentive (${RISK['incentive_per_customer_minute']:.2f} a customer minute "
            f"off supply) plus ${RISK['kw_per_customer'] * RISK['tariff_per_kwh']:.2f} of network revenue on energy not delivered "
            f"({RISK['kw_per_customer']:g} kW a customer at ${RISK['tariff_per_kwh']:.2f} a kWh). These are illustrative rates. A distributor's own rates come from its AER determination.")


# The severities that make a situation an alert: the Alerts list, its rail badge and every count of open situations use this one set
ALERT_SEVERITIES = ["critical", "high", "medium"]


def _release_due():
    """Every situation's queued messages whose undo window has passed go out (the list is polled every few seconds, so they go on time)."""
    pg.q("""UPDATE atlas.notification_outbox o SET status = 'sent', sent_at = now() FROM atlas.recommendation r
            WHERE r.rec_id = o.rec_id AND o.status = 'queued' AND o.situation_id IS NOT NULL AND r.kind = 'notify'
              AND r.status IN ('accepted', 'adjusted') AND r.decided_at < now() - make_interval(secs => %s)""", (UNDO_SEND_S,))


def listing(status="open"):
    """The alerts, most urgent first: the open situations (or all of them) of the alert severities, with their impact and revenue at risk."""
    _release_due()
    where = ("s.status <> 'closed'" if status == "open" else "true") + " AND s.severity = ANY(%(sev)s)"
    rows = pg.q(f"""SELECT s.situation_id, s.type, s.title, s.severity, s.status, s.detected_at, s.updated_at, s.headline, s.customers, s.payload,
                    s.life_support, s.critical_sites, s.feeders, o.customers_off, o.life_support_off, o.etr
                    FROM atlas.situation s LEFT JOIN atlas.live_outage o ON s.type = 'trip' AND o.outage_id = s.payload ->> 'outage_id'
                    WHERE {where}
                    ORDER BY CASE s.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, s.updated_at DESC LIMIT 60""",
                {"sev": ALERT_SEVERITIES})
    for r in rows:
        r["type_label"] = TYPE_LABEL.get(r["type"], r["type"])
        r["status_label"], r["status_note"] = STAGE.get(r["status"], (0, r["status"], ""))[1], stage_note(r["status"])
        r["customers_impacted"], r["risk_hours"], r["revenue_at_risk"] = _at_risk(r)
        r["life_support_impacted"] = int(r["life_support_off"] if r.get("life_support_off") is not None else (r.get("life_support") or 0))
        r["risk_basis"] = risk_basis(r)
        r.pop("etr", None)
    return rows


# ---------------------------------------------------------------------------------------------- detail per type
UNDO_SEND_S = 60   # a sent notice can be taken back for this long; after it the queued messages go out and Undo is closed


def _release_sent(sid):
    """Messages whose undo window has passed leave the outbox (delivery is simulated, so they are marked sent)."""
    pg.q("""UPDATE atlas.notification_outbox SET status = 'sent', sent_at = now() WHERE situation_id = %s AND status = 'queued' AND EXISTS (
              SELECT 1 FROM atlas.recommendation r WHERE r.situation_id = %s AND r.kind = 'notify' AND r.status IN ('accepted', 'adjusted')
              AND r.decided_at < now() - make_interval(secs => %s))""", (sid, sid, UNDO_SEND_S))


def detail(sid):
    _release_sent(sid)
    s = _sit(sid)
    s["type_label"] = TYPE_LABEL.get(s["type"], s["type"])
    s["events"] = pg.q("SELECT ts, actor, action, detail FROM atlas.situation_event WHERE situation_id = %s ORDER BY ts", (sid,))
    # undo_left: seconds left to take back a sent notice, timed by the database clock so a skewed browser clock cannot stretch it
    s["recommendations"] = pg.q("""SELECT *, CASE WHEN kind = 'notify' AND status IN ('accepted', 'adjusted')
                                   THEN greatest(0, %s - extract(epoch FROM now() - decided_at))::int END AS undo_left
                                   FROM atlas.recommendation WHERE situation_id = %s ORDER BY created_at""", (UNDO_SEND_S, sid))
    s["outbox"] = pg.q("""SELECT coalesce(a.label, o.audience) audience, o.channel, coalesce(t.label, o.status) status, count(*) n, min(o.body) sample
                          FROM atlas.notification_outbox o
                          LEFT JOIN ref_label a ON a.domain = 'audience' AND a.code = o.audience
                          LEFT JOIN ref_label t ON t.domain = 'outbox_status' AND t.code = o.status
                          WHERE o.situation_id = %s AND o.status <> 'draft' GROUP BY 1, 2, 3 ORDER BY 1""", (sid,))
    drafts = {d["rec_id"]: d["body"] for d in pg.q("""SELECT DISTINCT ON (rec_id) rec_id, body FROM atlas.notification_outbox
                                                         WHERE situation_id = %s AND status = 'draft'""", (sid,))}
    for r in s["recommendations"]:
        r["draft"] = drafts.get(r["rec_id"])   # the prepared SMS, so Adjust starts from the real wording
    _prepare_pending(s)
    fn = globals().get(f"_detail_{s['type']}")
    s["view"] = fn(s) if fn else {}
    return s


def _detail_overdue(s):
    """A crew gone quiet on an emergency: the watch (sent, expected, allowed, last heard), where the crew is now, the job, the situation it
    was sent to, and the crews who could take over."""
    p = s["payload"] or {}
    w = pg.q("SELECT * FROM rm.emergency_watch WHERE job_id = %s", (p.get("job_id"),), one=True) or {}
    job = pg.q("SELECT job_id, title, job_type, feeder_id, asset_id, customers, duration_min FROM rm.raised_job WHERE job_id = %s", (p.get("job_id"),), one=True) or {}
    linked = pg.q("SELECT situation_id, title, status, severity FROM atlas.situation WHERE situation_id = %s", (p.get("linked_situation_id"),), one=True) \
        if p.get("linked_situation_id") else None
    outage = pg.q("SELECT outage_id, status, customers_off, life_support_off, device_id FROM atlas.live_outage WHERE outage_id = %s", (p.get("outage_id"),), one=True) \
        if p.get("outage_id") else None
    pos = next((x for x in RP.positions(p.get("region_id")) if x["crew_id"] == p.get("crew_id")), None) if p.get("region_id") else None
    crews = [c for c in (RX.nearest(job["job_id"]) if job else []) if c["crew_id"] != p.get("crew_id")]
    if job:
        job["job_type_label"] = RD.label("job_type", job["job_type"])
    return dict(watch=w, job=job, linked=linked, outage=outage, crew=pos, crews=crews,
                last_status_label=RD.label("ops_job_status", w.get("last_status")) if w.get("last_status") else None)


def _detail_trip(s):
    p = s["payload"] or {}
    o = pg.q("SELECT * FROM atlas.live_outage WHERE outage_id = %s", (p.get("outage_id"),), one=True) or {}
    fid = (s["feeders"] or [None])[0]
    devices = pq("""SELECT a.asset_id, a.asset_class_label, a.lat, a.lon, a.customers_downstream, a.life_support_downstream
                    FROM gld_asset a WHERE a.feeder_id = %(f)s AND a.asset_class IN ('cb', 'recloser', 'gas_switch', 'nop')
                    ORDER BY a.customers_downstream DESC""", {"f": fid}, ttl=3600)
    job = _outage_job(p.get("outage_id"), o)
    crews = RX.nearest(job["job_id"]) if job else []
    calls = pq("SELECT count(*) n FROM live.call WHERE ts > now() - interval '2 hours'", ttl=20)
    alarms = pq("SELECT event_code, count(*) n FROM live.meter_event WHERE ts > now() - interval '2 hours' GROUP BY event_code", ttl=20)
    # backup: can the customers still off be back-fed through a tie, and is the zone substation itself on a single supply circuit?
    ties = pq("""SELECT t.nop, t.tie_feeder, t.other_zone, t.transfer_amps, t.transfer_share, t.feeder_peak_amps, b.rating_amps tie_rating_amps,
                        b.amps_peak_30d tie_peak_amps FROM gld_feeder_tie t JOIN gld_feeder_state b ON b.feeder_id = t.tie_feeder
                 WHERE t.feeder_id = %(f)s ORDER BY t.transfer_amps DESC""", {"f": fid}, ttl=3600)
    off = int(o.get("customers_off") or 0)
    # a fuse protects a spur: nothing below it can be back-fed through a feeder tie, so the feeder's ties do not apply
    spur = bool(pq("SELECT 1 FROM gld_asset WHERE asset_id = %(d)s AND asset_class = 'fuse'", {"d": o.get("device_id") or ""}, ttl=3600))
    if spur:
        ties = []
    for t in ties:   # how many of the customers still off this tie could carry, from the other feeder's spare emergency capacity
        t["customers_carried"] = int(round(off * float(t["transfer_share"] or 0)))
    zone = pq("SELECT zs_name, supply_circuits, single_fed FROM gld_zone_supply WHERE zs_id = concat('ZS-', substring(%(f)s, 1, 3))",
              {"f": fid}, ttl=3600)
    cause = pq("SELECT label FROM ref_label WHERE domain = 'cause' AND code = %(c)s", {"c": o.get("cause_code")}, ttl=3600)
    o["cause_label"] = cause[0]["label"] if cause else "Under investigation"
    steps = {r["code"]: r["label"] for r in pq("SELECT code, label FROM ref_label WHERE domain = 'outage_stage'", ttl=3600)}
    for x in o.get("stages") or []:
        x["label"] = steps.get(x.get("action"), "Update")
    return dict(outage=o, devices=devices, crews=crews, job=job, calls=calls[0]["n"] if calls else 0,
                meter_alarms={r["event_code"]: r["n"] for r in alarms}, backup=dict(ties=ties, spur=spur, zone=zone[0] if zone else None))


def _outage_job(oid, o):
    """The live outage's emergency job in resource management, with whoever has it and where it is up to. An active outage whose job is
    missing (the day's work was re-seeded under it) gets its job back, so a crew can always be sent."""
    job = RD.jobs_by_id().get(f"JOB-{oid}")
    if not job and o.get("status") == "active" and o.get("lat") is not None:
        RX.raise_job("FAULT_RESPONSE", f"Fault response {o['feeder_id']}: patrol and repair downstream of {o['device_id']}", o["lat"], o["lon"],
                     "detector", feeder_id=o["feeder_id"], asset_id=o["device_id"], priority=5, customers=o.get("customers_off") or 0,
                     source="outage", situation_id=o.get("situation_id"), emergency=True, job_id=f"JOB-{oid}")
        job = RD.jobs_by_id().get(f"JOB-{oid}")
    if not job:
        return None
    region = job["region_id"]
    owner = RS.override_map(region).get(job["job_id"]) or {}
    return dict(job_id=job["job_id"], title=job["title"], region_id=region, crew_id=owner.get("crew_id"), crew_name=owner.get("crew_name"),
                field_status=RS.field_status_region(region).get(job["job_id"]))


# the fire's shape as a geography (metres): the NSW RFS perimeter, or the point of a hotspot or incident
FIRE_SHAPE = {
    "perimeter": "ST_GeomFromGeoJSON(%(perimeter)s)::geography",
    "point": "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography",
}
# the span geography is the indexed expression ST_GeogFromText(geom_wkt) (index gld_asset_shape), so the join can use it
FIRE_SQL = """-- Spans within 10 km of the fire, by distance band, with the customers behind them (PostGIS, metres on the geography)
WITH fire AS (SELECT {fire} AS g),
spans AS (SELECT asset_id, feeder_id, customers_downstream, ST_GeogFromText(geom_wkt) AS g
          FROM gld_asset WHERE asset_class = 'overhead_span' AND geom_wkt IS NOT NULL)
SELECT s.feeder_id,
       CASE WHEN ST_Distance(f.g, s.g) <= 1000 THEN '0-1 km' WHEN ST_Distance(f.g, s.g) <= 3000 THEN '1-3 km'
            WHEN ST_Distance(f.g, s.g) <= 5000 THEN '3-5 km' ELSE '5-10 km' END AS band,
       count(*) AS spans, max(s.customers_downstream) AS customers_behind, round(min(ST_Distance(f.g, s.g))) AS nearest_m
FROM fire f JOIN spans s ON ST_DWithin(f.g, s.g, 10000)
GROUP BY s.feeder_id, band ORDER BY nearest_m"""


def _inline(sql_text, args):
    """The query with its values written in, for the 'Show the spatial query' panel (the query that runs keeps them as parameters)."""
    for k, v in args.items():
        lit = "'" + v.replace("'", "''") + "'" if isinstance(v, str) else str(v)
        sql_text = sql_text.replace(f"%({k})s", lit)
    return sql_text


def _detail_fire(s):
    p = s["payload"] or {}
    if str(p.get("fire_id", "")).startswith("HS-"):
        h = pq("SELECT * FROM gld_hotspot WHERE hotspot_id = %(i)s", {"i": p["fire_id"][3:]}, ttl=300)
        fire = dict(h[0], title=f"{h[0]['satellite']} hotspot", source="DEA Hotspots (Geoscience Australia)") if h else {}
    else:
        f = pq("""SELECT incident_id, title, alert_level, status, incident_type, size, lat, lon, perimeter_geojson, updated, captured_at
                  FROM gld_fire_incident WHERE incident_id = %(i)s""", {"i": p.get("fire_id")}, ttl=300)
        fire = dict(f[0], source="NSW Rural Fire Service") if f else {}
    if fire.get("lat") is None and not fire.get("perimeter_geojson"):
        # the fire has dropped out of the live feed (out, or the hotspot aged off): keep the feeders it was detected on
        fire = dict(source=p.get("source"), alert_level=p.get("alert_level"), status="No longer in the live feed.", incident_type=p.get("incident_type"))
        spatial_sql, bands, feeders = None, [], sorted(s.get("feeders") or [])
    else:
        # coordinates and the perimeter travel as parameters; only the fixed shape expression is chosen in code
        if fire.get("perimeter_geojson"):
            shape, args = "perimeter", dict(perimeter=fire["perimeter_geojson"])
        else:
            shape, args = "point", dict(lon=fire["lon"], lat=fire["lat"])
        sql_text = FIRE_SQL.format(fire=FIRE_SHAPE[shape])
        bands = pq(sql_text, args, ttl=300)
        spatial_sql = _inline(sql_text, args)
        feeders = sorted({b["feeder_id"] for b in bands if b["band"] in ("0-1 km", "1-3 km")})
    fd = pq("""SELECT feeder_id, feeder_name, customers, life_support, critical_sites, bushfire_share, veg_cat_a, status
               FROM gld_feeder_state WHERE feeder_id = ANY(%(f)s)""", {"f": feeders}, ttl=300) if feeders else []
    risk = pq("""SELECT r.span_id, r.feeder_id, round(r.fault_probability::numeric, 3) p, v.clearance_m, v.hazard_tree, v.dominant_species
                 FROM ml_span_risk r JOIN gld_vegetation_risk v USING (span_id)
                 WHERE r.feeder_id = ANY(%(f)s) ORDER BY r.fault_probability DESC LIMIT 8""", {"f": feeders}, ttl=300) if feeders else []
    danger = fire_danger()
    return dict(fire=fire, bands=bands, feeders=fd, span_risk=risk, spatial_sql=spatial_sql, fire_danger=danger)


FOOTPRINT_COUNCILS = ("Blue Mountains", "Penrith", "Central Coast", "Hawkesbury", "Lithgow", "Blacktown", "Camden", "Liverpool", "Wollondilly")


def fire_danger():
    rows = pq("SELECT * FROM gld_fire_danger", ttl=600)
    out = [r for r in rows if any(c in (r["councils"] or "") for c in FOOTPRINT_COUNCILS)]
    order = ["NONE", "NO RATING", "MODERATE", "HIGH", "EXTREME", "CATASTROPHIC"]
    for r in out:
        r["rank_today"] = order.index(r["rating_today"]) if r["rating_today"] in order else 0
        r["rank_tomorrow"] = order.index(r["rating_tomorrow"]) if r["rating_tomorrow"] in order else 0
    return out


AREA_OF = {"NPF": "MET", "RPR": "MET", "IBH": "MET", "CTC": "MET", "BRP": "MET", "MGF": "MET", "LPH": "RNG", "KNV": "RNG", "GVL": "RNG",
           "BWT": "CST", "TGS": "CST", "WBP": "CST"}
AREA_LABEL = {"MET": "Metro West", "RNG": "Ranges", "CST": "Coast"}


def _detail_storm(s):
    proj = pq("SELECT * FROM ml_storm_projection", ttl=300)   # the storm model's feeder-day projection on the live forecast
    days = {}
    for r in proj:
        d = days.setdefault(r["day"], dict(day=r["day"], faults=0.0, customers=0.0, crew_hours=0.0, crew_hours_p90=0.0, peak_gust=0.0))
        d["faults"] += r["faults_p50"]
        d["customers"] += r["customers_p50"]
        d["crew_hours"] += r["crew_hours_p50"]
        d["crew_hours_p90"] += r["crew_hours_p90"]
        d["peak_gust"] = max(d["peak_gust"], r["gust_kmh"])
    peak_day = max(days.values(), key=lambda d: d["faults"])["day"] if days else None
    top = sorted([r for r in proj if r["day"] == peak_day], key=lambda r: -r["faults_p50"])[:10]
    fs = {f["feeder_id"]: f for f in pq("SELECT feeder_id, feeder_name, customers, life_support, critical_sites, veg_cat_a, open_defects "
                                        "FROM gld_feeder_state", ttl=3600)}
    for t in top:
        t.update({k: fs.get(t["feeder_id"], {}).get(k) for k in ("feeder_name", "customers", "life_support", "critical_sites", "veg_cat_a", "open_defects")})
        t["drivers"] = json.loads(t["drivers"]) if isinstance(t["drivers"], str) else t["drivers"]
    # Demand against capacity by area on the peak day. P90 of the area total treats faults as a compound Poisson process:
    # variance = sum over feeders of expected faults x (crew-hours per fault) squared x 1.5 (spread of repair effort).
    need = {}
    for r in proj:
        if r["day"] == peak_day:
            a = AREA_OF.get(r["feeder_id"][:3], "MET")
            lam = max(1e-6, float(r["faults_p50"]))
            sev = float(r["crew_hours_p50"]) / lam
            v = need.setdefault(a, [0.0, 0.0])
            v[0] += float(r["crew_hours_p50"])
            v[1] += lam * sev * sev * 1.5
    cap = _line_crews_by_area()
    capacity = [dict(area=a, area_label=AREA_LABEL[a], need_p50=round(v[0]), need_p90=round(v[0] + 1.28 * math.sqrt(v[1])), available=cap.get(a, 0) * 10,
                     crews=cap.get(a, 0)) for a, v in sorted(need.items())]
    h6 = pq("""SELECT window_start, round(max(max_gust_kmh)::numeric) gust, round(sum(faults_p50)::numeric, 2) faults
               FROM ml_storm_projection_6h GROUP BY 1 ORDER BY 1""", ttl=300)
    cells = pq("""SELECT h3, geo, round(max(gust_kmh)::numeric) gust, round(sum(rain_mm)::numeric, 1) rain
                  FROM gld_weather_cell WHERE (forecast_time AT TIME ZONE 'Australia/Sydney')::date = %(d)s GROUP BY h3, geo""",
               {"d": peak_day}, ttl=600) if peak_day else []
    return dict(days=sorted(days.values(), key=lambda d: d["day"]), peak_day=peak_day, top_feeders=top, capacity=capacity, windows=h6,
                cells=cells, model_version=proj[0]["model_version"] if proj else None,
                ls_on_top=sum((t.get("life_support") or 0) for t in top))


def _line_crews_by_area():
    """Line crews (the ones who repair storm damage) on the roster today in each area, not stood down."""
    e, sd = RD.eng(), {c for _, c, _ in RS.stood_down_all()}
    area = {r["region_id"]: r["area"] for r in RD.data()["regions"]}
    out = {}
    for c in e.crews.values():
        if "LV_HV" in e.skills.get(c["crew_id"], set()) and c["crew_id"] not in sd and c["crew_id"] not in e.on_leave:
            out[area[c["region_id"]]] = out.get(area[c["region_id"]], 0) + 1
    return out


def _detail_heat(s):
    p = s["payload"] or {}
    tx = p.get("transformer_id")
    zs = p.get("zs_id")
    # the historian marts end on the as-of day, so their windows run back from it (Sydney midnight), not from the wall clock
    hist = pq("""SELECT hour t, attribute, round(value::numeric, 2) v FROM gld_historian_hourly
                 WHERE element = %(e)s AND hour >= %(s)s ORDER BY t""",
              {"e": tx, "s": dt.datetime.combine(core.AS_OF - dt.timedelta(days=30), dt.time(0), tzinfo=ZoneInfo("Australia/Sydney"))}, ttl=1800)
    dga = pq("""SELECT day d, round(value::numeric) h2 FROM gld_historian_daily
                WHERE element = %(e)s AND attribute = 'H2_PPM' AND day >= %(s)s ORDER BY d""", {"e": tx, "s": core.AS_OF - dt.timedelta(days=120)}, ttl=3600)
    fc = pq("SELECT * FROM ml_zone_load_forecast WHERE zs_id = %(z)s ORDER BY day", {"z": zs}, ttl=1800)
    # the backup ties of this zone's feeders, from the other feeder's side: the tie's other feeder, its zone and its spare capacity
    ties = pq("""SELECT t.nop, f1.feeder_id from_feeder, f2.feeder_id to_feeder, f2.zs_id to_zs, f2.amps_peak_30d to_peak_amps, f2.rating_amps to_rating
                 FROM gld_feeder_tie t
                 JOIN gld_feeder_state f1 ON f1.feeder_id = t.feeder_id AND f1.zs_id = %(z)s
                 JOIN gld_feeder_state f2 ON f2.feeder_id = t.tie_feeder
                 ORDER BY f2.zs_id <> %(z)s DESC, f2.amps_peak_30d / NULLIF(f2.rating_amps, 0)""", {"z": zs}, ttl=3600)
    state = pq("SELECT * FROM gld_zone_transformer_state WHERE transformer_id = %(t)s", {"t": tx}, ttl=600)
    live = (pg.q("SELECT value -> %s v FROM app.live_state WHERE key = 'zone_transformers'", (tx,), one=True) or {}).get("v")
    return dict(history=hist, dga=dga, forecast=fc, ties=ties[:4], state=state[0] if state else {}, live=live)


def _detail_min_load(s):
    sun = pq("""SELECT date_trunc('hour', forecast_time) t, round(avg(radiation_wm2)) rad FROM gld_weather_cell
                WHERE forecast_time >= now() - interval '6 hours' GROUP BY 1 ORDER BY 1 LIMIT 72""", ttl=900)
    ds = pq("""SELECT ds_id, feeder_id, zs_id, kva, customers, solar_kw, est_midday_reverse_flow, lat, lon FROM gld_ds_state
               ORDER BY est_midday_reverse_flow DESC LIMIT 12""", ttl=1800)
    # solar sites per distribution substation in one grouped query (substations with none count 0)
    sites = {r["ds_id"]: r["n"] for r in pq("""SELECT ds_id, count(*) n FROM gld_nmi WHERE ds_id = ANY(%(ds)s) AND solar_kw > 0
                                              GROUP BY ds_id""", {"ds": [d["ds_id"] for d in ds]}, ttl=3600)}
    for d in ds:
        d["recommended_limit_kw"] = 1.5 if d["est_midday_reverse_flow"] > 0.8 else 3.0
        d["sites"] = sites.get(d["ds_id"], 0)
    return dict(solar=sun, transformers=ds)


def _detail_vegetation(s):
    pk = pq("""SELECT feeder_id, count(*) FILTER (WHERE status = 'red') red, count(*) FILTER (WHERE status = 'amber') amber,
                      round(sum(risk_score)::numeric) risk, max(customers_downstream) customers, avg(bushfire_prone::int) bushfire
               FROM gld_vegetation_risk GROUP BY feeder_id HAVING count(*) FILTER (WHERE status = 'red') > 0
               ORDER BY risk DESC LIMIT 15""", ttl=3600)
    spans = pq("""SELECT span_id, feeder_id, clearance_m, clearance_12m_m, category_label, dominant_species, hazard_tree, bushfire_prone, lat, lon
                  FROM gld_vegetation_risk WHERE status = 'red' ORDER BY risk_score DESC LIMIT 12""", ttl=3600)
    return dict(packages=pk, spans=spans)


def _detail_drone_defect(s):
    p = s["payload"] or {}
    pole = p.get("pole_id")
    a = pq("SELECT * FROM gld_asset WHERE asset_id = %(p)s", {"p": pole}, ttl=3600)
    a = a[0] if a else {}
    det = pq("SELECT * FROM gld_drone_detection WHERE detection_id = %(d)s", {"d": p.get("detection_id")}, ttl=3600)
    span = pq("SELECT protective_device, customers_downstream FROM gld_asset WHERE asset_id = %(s)s", {"s": "S" + (pole or "P")[1:]}, ttl=3600)
    span = span[0] if span else {}
    a["protective_device"] = span.get("protective_device")
    a["customers_downstream"] = span.get("customers_downstream", a.get("customers_downstream"))
    bundle = pq("""SELECT d.notification_id, d.asset_id, d.defect_label, d.priority, d.age_days, sp.customers_downstream
                   FROM gld_open_defect d JOIN gld_asset sp ON sp.asset_id = concat('S', substring(d.asset_id, 2))
                   WHERE sp.protective_device = %(pd)s
                     AND NOT EXISTS (SELECT 1 FROM atlas.defect_closure c WHERE c.notification_id = d.notification_id)
                   ORDER BY d.priority LIMIT 8""", {"pd": a.get("protective_device")}, ttl=60) if a.get("protective_device") else []
    return dict(pole=a, detection=det[0] if det else {}, bundle=bundle, image=f"/static/drone/{'cracked' if 'cross' in (p.get('defect') or '') else 'defect'}.png")


def _detail_fatigue(s):
    """Crews whose day runs past the enterprise agreement cap (authorised overtime, or a callout on top of a full day)."""
    e, out = RD.eng(), []
    for region in e.regions:
        pos = {p["crew_id"]: p for p in RP.positions(region)}
        for cid, stops in RP.field_days(region).items():
            c = e.crews[cid]
            t = RP.schedule(c, stops)
            planned = (t[-1]["finish"] - RP.DAY_SHIFT) / 60 if t else 0
            if planned * 60 > e.cap_min(c) and pos.get(cid, {}).get("status") != "stood_down":
                out.append(dict(crew_id=cid, crew_name=c["crew_name"], region_id=region, region=RD.region_name(region), hours=round(planned, 1),
                                hours_on_shift=RP.hours_on_shift(pos.get(cid)), status=pos.get(cid, {}).get("status_label")))
    return dict(crews=sorted(out, key=lambda x: -x["hours"]))


def _detail_records(s):
    return dict(meters=pq("SELECT * FROM gld_dq_meter_mismatch ORDER BY best_correlation DESC LIMIT 40", ttl=3600))


# ---------------------------------------------------------------------------------------------- plans
def plan(sid, actor="system"):
    s = detail(sid)
    pg.q("""DELETE FROM atlas.notification_outbox WHERE status = 'draft' AND rec_id IN (
              SELECT rec_id FROM atlas.recommendation WHERE situation_id = %s AND status = 'proposed')""", (sid,))
    pg.q("DELETE FROM atlas.recommendation WHERE situation_id = %s AND status = 'proposed'", (sid,))
    recs = []
    v = s["view"]
    t = s["type"]
    if t == "storm":
        cap = v["capacity"]
        for c in cap:
            if c["need_p90"] < 8:
                continue
            short = max(0, c["need_p90"] - c["crews"] * 10)
            if short > 0:
                n = min(15, math.ceil(short / 10))
                recs.append(_rec("contractors", f"Call in {_qty(n, 'contractor line crew')} to {c['area_label']}.",
                                 f"P90 demand exceeds the line crews already there by {_qty(short, 'crew-hour')}. The nearest contractor crews move "
                                 f"to the area, and their stops are re-rostered.", dict(area=c["area"], crews=n)))
        if v["top_feeders"]:
            top = v["top_feeders"][:5]
            recs.append(_rec("materials", "Pre-stage poles, cross-arms and conductor at the Ranges and Penrith depots.",
                             "Expected damage concentrates on " + ", ".join(t["feeder_id"] for t in top[:3]) + ".",
                             dict(items=[{"item": "Wood pole 12.5 m", "qty": 6}, {"item": "Cross-arm 2.4 m", "qty": 20}])))
            ls = sum((t.get("life_support") or 0) for t in top)
            recs.append(_rec("notify", f"Pre-notify {_qty(ls, 'life support customer')} on the 5 most exposed feeders.",
                             "This gives them time to arrange backup before the storm.", dict(feeders=[t["feeder_id"] for t in top], audience="life_support")))
    elif t == "fire":
        near = [b for b in v["bands"] if b["band"] in ("0-1 km", "1-3 km")]
        feeders = sorted({b["feeder_id"] for b in near})
        if feeders:
            recs.append(_rec("protection", f"Switch {_qty(len(feeders), 'feeder')} to fire-season protection (no auto-reclose).",
                             "This stops a fault from re-energising into dry fuel near the fire.", dict(feeders=feeders)))
            recs.append(_rec("patrol", f"Patrol the {_qty(sum(b['spans'] for b in near), 'span')} within 3 km before re-energising after any trip.",
                             "Ground or drone patrol of the exposed spans.", dict(feeders=feeders)))
            cust = sum(f["customers"] or 0 for f in v["feeders"])
            ls = sum(f["life_support"] or 0 for f in v["feeders"])
            recs.append(_rec("notify", f"Warn {_qty(cust, 'customer')} on the exposed feeders, with {_qty(ls, 'life support customer')} first.",
                             "Supply may be interrupted if the fire reaches the lines.", dict(feeders=feeders, audience="all")))
            recs.append(_rec("deenergise_case", "Prepare a de-energisation case for the segment nearest the fire.",
                             "The decision rests with the duty manager. The case lists the customers and life support sites affected.",
                             dict(feeders=feeders[:2])))
    elif t == "trip":
        o = v["outage"]
        c = next((c for c in v["crews"] if not c["blocked"]), None)
        if c and o.get("status") == "active" and v.get("job"):
            recs.append(_rec("dispatch", f"Dispatch {c['crew_name']}, {c['persona'].lower()} ({c['km']} km, about {c['eta_min']} min).",
                             f"The nearest qualified crew with hours left today and no open safety incident. {_still_off(o.get('customers_off', 0))}.",
                             dict(crew_id=c["crew_id"], job_id=v["job"]["job_id"])))
            reach = len(_recipients("affected", s.get("feeders"), o.get("device_id")))
            recs.append(_rec("notify", f"Send the restoration update to {_qty(reach, 'customer')} the outage interrupted.",
                             f"Of these, {_still_off(o.get('customers_off', 0))}; the rest are back on through switching. The estimated restoration "
                             "time comes from the time-to-restore model.", dict(audience="affected", outage_id=o.get("outage_id"), device_id=o.get("device_id"))))
    elif t == "heat":
        st = v["state"]
        if v["ties"]:
            tie = v["ties"][0]
            recs.append(_rec("switching", f"Transfer load from {tie['from_feeder']} to {tie['to_feeder']} through {tie['nop']}.",
                             "Takes peak load off the hot transformer through a normally open point.", dict(nop=tie["nop"], to=tie["to_feeder"])))
        recs.append(_rec("oil_sample", f"Take an oil sample and repeat dissolved gas analysis (DGA) on {st.get('transformer_id')} this week.",
                         f"Hydrogen has risen {st.get('h2_rise_90d')} ppm in 90 days.", dict(asset=st.get("transformer_id"))))
        recs.append(_rec("substation_crew", f"Send a substation crew to {st.get('transformer_id')} today.",
                         "Inspects the transformer and stays on call through the peak, in case the alarm limit is reached.",
                         dict(asset=st.get("transformer_id"), zs_id=(s["payload"] or {}).get("zs_id"))))
    elif t == "min_load":
        tx = v["transformers"][:8]
        recs.append(_rec("envelope", f"Publish lower export envelopes for {_qty(len(tx), 'high-solar transformer')} between 10:00 and 15:00.",
                         "This keeps LV voltage within limits. Customers keep self-consumption, and only export above the limit is curtailed.",
                         dict(transformers=[d["ds_id"] for d in tx], limit_kw=1.5)))
    elif t == "vegetation":
        for p in v["packages"][:6]:
            recs.append(_rec("package", f"Issue a cutting package for {p['feeder_id']}: {_qty(p['red'], 'span')} due now, {_qty(p['amber'], 'span')} due within 12 months.",
                             f"Risk score {p['risk']}. The bushfire-prone share is {round((p['bushfire'] or 0) * 100)} percent.", dict(feeder=p["feeder_id"], spans=p["red"])))
    elif t == "drone_defect":
        pole = v["pole"]
        recs.append(_rec("notification", f"Raise a priority 2 notification for {pole.get('asset_id')}.",
                         "Confirmed by the multimodal image check. The notification goes to the asset register.", dict(pole=pole.get("asset_id"))))
        if len(v["bundle"]) > 1:
            recs.append(_rec("bundle", f"Bundle {_qty(len(v['bundle']), 'job')} behind {pole.get('protective_device')} into one planned outage.",
                             f"This means one outage and one switching crew instead of {len(v['bundle'])} separate outages, and "
                             f"{_qty(pole.get('customers_downstream') or 0, 'customer')} notified once.",
                             dict(jobs=[b["notification_id"] for b in v["bundle"]], isolation=pole.get("protective_device"))))
    elif t == "fatigue":
        recs.append(_rec("reroster", f"Stand down {_qty(len(v['crews']), 'crew')} past the 12-hour cap and re-roster their stops.",
                         "Their remaining stops go to the nearest rested, qualified crews. Any stop that no one can take is put on hold, with the reason recorded.",
                         dict(crews=[dict(crew_id=c["crew_id"], region_id=c["region_id"]) for c in v["crews"]])))
    elif t == "overdue":
        w, job = v["watch"], v["job"]
        if w and w.get("status") != "closed":
            recs.append(_rec("welfare", f"Call {w['crew_name']} to check they are safe and get an update.",
                             "A crew that goes quiet on an emergency may be hurt or out of signal, so its safety comes first. Logging the call gives "
                             f"the crew {OD.WELFARE_EXTEND_MIN} minutes to report before the alert escalates.", dict(job_id=w["job_id"])))
            c = next((c for c in v["crews"] if not c["blocked"]), None)
            if c and job:
                recs.append(_rec("dispatch", f"Send {c['crew_name']}, {c['persona'].lower()}, to take over ({c['km']} km, about {c['eta_min']} min).",
                                 f"The nearest qualified crew with hours left today. The job moves to them, and {w['crew_name']}'s watch closes.",
                                 dict(crew_id=c["crew_id"], job_id=job["job_id"])))
            o = v["outage"]
            if o and o.get("status") == "active":
                reach = len(_recipients("affected", s.get("feeders"), o.get("device_id")))
                recs.append(_rec("notify", f"Tell {_qty(reach, 'customer')} the restoration is running late.",
                                 f"{_still_off(o.get('customers_off', 0))}. The crew has not reported, so the restoration time is no longer firm.",
                                 dict(audience="affected", outage_id=o.get("outage_id"), device_id=o.get("device_id"))))
    elif t == "records":
        recs.append(_rec("correction", f"Raise {_qty(len(v['meters']), 'correction')} to the governed master data workflow.",
                         "Move each meter to the transformer its voltage follows, after a field check.", dict(meters=[m["nmi"] for m in v["meters"]])))
    for r in recs:
        pg.q("""INSERT INTO atlas.recommendation (rec_id, situation_id, kind, title, detail, payload) VALUES (%s,%s,%s,%s,%s,%s)""",
             (r["rec_id"], sid, r["kind"], r["title"], r["detail"], json.dumps(r["payload"], default=str)))
    pg.q("UPDATE atlas.situation SET status = CASE WHEN status = 'detected' THEN 'plan_ready' ELSE status END, updated_at = now() "
         "WHERE situation_id = %s", (sid,))
    pg.q("INSERT INTO atlas.situation_event (situation_id, actor, action, detail) VALUES (%s,%s,'plan_ready',%s)",
         (sid, actor, json.dumps(dict(recommendations=len(recs)))))
    return detail(sid)


def _rec(kind, title, detail_text, payload):
    return dict(rec_id=f"REC-{uuid.uuid4().hex[:10]}", kind=kind, title=title, detail=detail_text, payload=payload)


# ---------------------------------------------------------------------------------------------- decisions and write-back
def decide(sid, rec_id, action, actor, reason=None, crew_id=None, message=None, audience=None):
    r = pg.q("SELECT * FROM atlas.recommendation WHERE rec_id = %s AND situation_id = %s", (rec_id, sid), one=True)
    if not r:
        raise NotFound(rec_id)
    if crew_id and r["kind"] == "dispatch":   # adjusted to another crew from the dispatch list
        r["payload"] = dict(r["payload"] or {}, crew_id=crew_id)
        _remember(r, crew_id=crew_id)
        c = RD.crew(crew_id) or {}
        pg.q("UPDATE atlas.recommendation SET title = %s WHERE rec_id = %s", (f"Dispatch {c.get('crew_name', crew_id)}, {(c.get('persona') or 'crew').lower()}.", rec_id))
    if r["kind"] == "notify" and (message or audience):   # adjusted: the dispatcher's own wording, and who it goes to
        r["payload"] = dict(r["payload"] or {}, **{k: v for k, v in (("body", message), ("audience", audience)) if v})
        _remember(r, **{k: v for k, v in (("body", message), ("audience", audience)) if v})
    status = {"accept": "accepted", "reject": "rejected", "adjust": "adjusted"}[action]
    pg.q("UPDATE atlas.recommendation SET status = %s, decided_at = now(), decided_by = %s, reason = %s WHERE rec_id = %s",
         (status, actor, reason, rec_id))
    effects = []
    if action == "reject" and r["kind"] == "notify":
        pg.q("DELETE FROM atlas.notification_outbox WHERE rec_id = %s AND status = 'draft'", (rec_id,))
    if action in ("accept", "adjust"):
        effects = _execute(sid, r, actor)
    pg.q("INSERT INTO atlas.situation_event (situation_id, actor, action, detail) VALUES (%s,%s,%s,%s)",
         (sid, actor, status, json.dumps(dict(rec=r["title"], reason=reason, effects=effects), default=str)))
    pending = pg.q("SELECT count(*) n FROM atlas.recommendation WHERE situation_id = %s AND status = 'proposed'", (sid,), one=True)["n"]
    pg.q("""UPDATE atlas.situation SET status = CASE WHEN %s = 0 THEN 'executing' ELSE 'decided' END, owner = coalesce(owner, %s), updated_at = now()
            WHERE situation_id = %s AND status NOT IN ('closed')""", (pending, actor, sid))
    core.audit(actor, f"recommendation_{status}", rec_id, dict(situation=sid, effects=effects))
    return dict(status=status, effects=effects)


def undo(sid, rec_id, actor):
    """Take a decision back: the recommendation returns to proposed and what it set in motion is reversed where it can be."""
    r = pg.q("SELECT * FROM atlas.recommendation WHERE rec_id = %s AND situation_id = %s", (rec_id, sid), one=True)
    if not r:
        raise NotFound(rec_id)
    p, k, out = r["payload"] or {}, r["kind"], []
    if k == "notify" and r["status"] in ("accepted", "adjusted") and pg.q(
            "SELECT decided_at < now() - make_interval(secs => %s) late FROM atlas.recommendation WHERE rec_id = %s", (UNDO_SEND_S, rec_id), one=True)["late"]:
        raise ValueError("The messages have already gone out, so this notice can no longer be undone.")
    if r["status"] in ("accepted", "adjusted"):
        if k == "dispatch":
            RX.recall(p.get("job_id"), actor)
            out.append(f"{(RD.crew(p.get('crew_id')) or {}).get('crew_name', p.get('crew_id'))} recalled from the job.")
        elif k == "notify":
            n = pg.q("UPDATE atlas.notification_outbox SET status = 'draft' WHERE rec_id = %s AND status = 'queued' RETURNING msg_id", (rec_id,))
            out.append(f"{_qty(len(n), 'unsent message')} withdrawn.")
        elif k == "contractors":
            for cid in p.get("moved") or []:
                RX.reset_region(cid, actor)
            out.append(f"{_qty(len(p.get('moved') or []), 'contractor crew')} sent back to their regions.")
        elif k in ("patrol", "package", "bundle", "substation_crew"):
            n = RX.delete_jobs(p.get("jobs_raised") or [], actor)["withdrawn"]
            out.append(f"{_qty(n, 'job')} withdrawn.")
        elif k == "reroster":
            for c in p.get("crews", []):
                RX.restore_crew(c["region_id"], c["crew_id"], actor)
            out.append(f"{_qty(len(p.get('crews', [])), 'crew')} back on the roster with their stops.")
        elif k == "switching":
            out.append(_control("SWITCH", p.get("nop"), {"action": "open", "undo": True}))
        elif k == "protection":
            out += [_control("SETTING_CHANGE", f"{f}-CB01", {"auto_reclose": "on", "undo": True}) for f in p.get("feeders", [])]
        elif k == "envelope":
            out.append("Export limits back to their defaults")
        elif k == "welfare":
            out.append("The welfare call stays on the record, and the crew keeps the time it was given.")
    pg.q("UPDATE atlas.recommendation SET status = 'proposed', decided_at = NULL, decided_by = NULL, reason = NULL WHERE rec_id = %s", (rec_id,))
    pg.q("INSERT INTO atlas.situation_event (situation_id, actor, action, detail) VALUES (%s,%s,'undone',%s)",
         (sid, actor, json.dumps(dict(rec=r["title"], was=r["status"], effects=out), default=str)))
    pg.q("UPDATE atlas.situation SET status = 'decided', updated_at = now() WHERE situation_id = %s AND status NOT IN ('closed')", (sid,))
    core.audit(actor, "recommendation_undone", rec_id, dict(situation=sid, was=r["status"], effects=out))
    return dict(status="undone", effects=out)


def _control(kind, target, payload):
    """A control action to the operational systems (simulated): the effect line the decision trail and the audit log record."""
    label = {"SWITCH": "Switching instruction", "SETTING_CHANGE": "Protection setting change", "ENVELOPE": "Export envelope"}.get(kind, kind)
    detail = ", ".join(k.replace("_", " ") + " " + str(v) for k, v in payload.items())
    return f"{label} sent to {target} ({detail})"


def _execute(sid, r, actor):
    p = r["payload"] or {}
    k = r["kind"]
    s = _sit(sid)
    out = []
    if k == "dispatch":
        d = RX.dispatch(p["job_id"], p["crew_id"], actor)
        out.append(f"{d['crew_name']} dispatched, {d['km']} km away. The job is first on their field app." if d.get("ok") else d.get("error", "Not dispatched."))
    elif k == "notify":
        out.append(queue_notifications(s, r, p.get("audience", "affected"), p.get("feeders") or s["feeders"], p.get("body")))
    elif k == "contractors":
        moved = _call_in_contractors(p.get("area"), int(p.get("crews", 2)), actor)
        _remember(r, moved=moved)
        out.append(f"{_qty(len(moved), 'contractor line crew')} moved to {AREA_LABEL.get(p.get('area'), p.get('area'))}: "
                   + ", ".join((RD.crew(c) or {}).get("crew_name", c) for c in moved) + ".")
    elif k == "protection":
        for f in p.get("feeders", []):
            out.append(_control("SETTING_CHANGE", f"{f}-CB01", {"auto_reclose": "off", "reason": "fire nearby"}))
    elif k == "patrol":
        raised = [_raise(sid, "ASSET_INSPECTION", f"Fire patrol {f} before re-energising", f, actor, priority=5, emergency=True)
                  for f in p.get("feeders", [])[:4]]
        _remember(r, jobs_raised=[j for j in raised if j])
        out.append(f"Patrol jobs on {_qty(len([j for j in raised if j]), 'feeder')}, scheduled first on the nearest inspection crews' days.")
    elif k == "switching":
        out.append(_control("SWITCH", p.get("nop"), {"action": "close", "transfer_to": p.get("to")}))
    elif k == "envelope":
        out.append(_control("ENVELOPE", ",".join(p.get("transformers", [])[:3]) + "...", {"opModExpLimW": p.get("limit_kw", 1.5) * 1000}))
    elif k == "package":
        j = _raise(sid, "VEGETATION", f"Cutting package {p['feeder']}: {p['spans']} spans", p["feeder"], actor, priority=4,
                   duration_min=min(480, 60 + int(p["spans"]) * 12))
        _remember(r, jobs_raised=[j] if j else [])
        out.append(f"Cutting package for {p['feeder']} added to the vegetation crews' plan.")
    elif k == "notification":
        out.append(f"Notification raised for {p.get('pole')} and sent to the asset register.")
    elif k == "bundle":
        pole = (s["payload"] or {}).get("pole_id")
        j = _raise(sid, "HV_MAINT", f"Planned outage behind {p.get('isolation')}: {len(p.get('jobs', []))} defects in one visit", None, actor,
                   priority=3, duration_min=min(480, 75 * len(p.get("jobs", []))), asset_id=pole)
        _remember(r, jobs_raised=[j] if j else [])
        out.append(f"{_qty(len(p.get('jobs', [])), 'job')} bundled behind {p.get('isolation')} into one job for a line crew.")
    elif k == "substation_crew":
        j = _raise(sid, "SUBSTATION_MAINT", f"Inspect {p.get('asset')} and stay on call through the peak", None, actor, priority=5,
                   emergency=True, asset_id=p.get("zs_id"))
        _remember(r, jobs_raised=[j] if j else [])
        out.append(f"Substation crew job raised for {p.get('asset')}.")
    elif k == "welfare":
        out.append(OD.welfare_check(p.get("job_id"), actor))
    elif k == "reroster":
        n = sum(RX.stand_down(c["region_id"], c["crew_id"], "Fatigue limit (12 hour cap)", actor)["count"] for c in p.get("crews", []))
        out.append(f"{_qty(len(p.get('crews', [])), 'crew')} stood down. {_qty(n, 'stop')} {'was' if n == 1 else 'were'} re-rostered to rested crews or put on hold.")
    else:
        out.append("Recorded")
    return out


def _remember(r, **kv):
    """Keep what an action did on the recommendation, so Undo can reverse exactly that."""
    pg.q("UPDATE atlas.recommendation SET payload = payload || %s::jsonb WHERE rec_id = %s", (json.dumps(kv, default=str), r["rec_id"]))


def _raise(sid, job_type, title, feeder_id, actor, priority=4, emergency=False, duration_min=None, asset_id=None):
    """A job for a situation, on the feeder (or the asset) it concerns."""
    if asset_id:
        a = pq("SELECT lat, lon, feeder_id FROM gld_asset WHERE asset_id = %(a)s", {"a": asset_id}, ttl=3600)
        if not a:
            return None
        lat, lon, feeder_id = a[0]["lat"], a[0]["lon"], feeder_id or a[0]["feeder_id"]
    else:
        f = next((f for f in RD.data()["feeders"] if f["feeder_id"] == feeder_id), None)
        if not f:
            return None
        lat, lon = f["lat"], f["lon"]
    return RX.raise_job(job_type, title, lat, lon, actor, feeder_id=feeder_id, asset_id=asset_id, priority=priority, source="situation",
                        situation_id=sid, emergency=emergency, duration_min=duration_min)["job_id"]


def _call_in_contractors(area, n, actor):
    """Move the nearest contractor line crews from other areas into the area's busiest region."""
    e = RD.eng()
    regions = RD.regions_in_area(area)
    if not regions:
        return []
    to = max(regions, key=lambda x: len(e.jobs_for_region(x)))
    target = next(r for r in RD.data()["regions"] if r["region_id"] == to)
    pool = [c for c in e.crews.values() if c["employment_type"] == "Contractor" and c["region_id"] not in regions
            and "LV_HV" in e.skills.get(c["crew_id"], set())]
    pool.sort(key=lambda c: RX.km(c["home_lat"], c["home_lon"], target["lat"], target["lon"]))
    moved = []
    for c in pool[:n]:
        if RX.move_region(c["crew_id"], to, actor).get("ok"):
            moved.append(c["crew_id"])
    return moved


def _recipients(audience, feeders, device=None):
    """Who a situation's notice reaches: the customers of the distribution substations behind the tripped device (gld_isolation_ds, the
    trace the customer notices use), else every customer on the feeders; only the life support customers for that audience."""
    where = "AND n.life_support" if audience == "life_support" else ""
    if device:
        got = pq(f"""SELECT n.nmi, n.life_support FROM gld_nmi n JOIN gld_isolation_ds d ON d.ds_id = n.ds_id
                     WHERE d.device_id = %(d)s {where}""", {"d": device}, ttl=600)
        if got:
            return got
    feeders = [f for f in (feeders or []) if f]
    return pq(f"SELECT n.nmi, n.life_support FROM gld_nmi n WHERE n.feeder_id = ANY(%(f)s) {where}", {"f": feeders}, ttl=600)


def _draft_sms(s, audience):
    return core.write(json.dumps(dict(situation=s["title"], headline=s["headline"], audience=audience)),
                      "Write one SMS (under 300 characters) from the electricity distributor to affected customers. Do not name the company. Plain, calm, specific about what may "
                      "happen and what to do; life support customers get an extra line to enact their backup plan. No dashes as punctuation.",
                      max_tokens=2000)   # the fast model reasons for ~550 tokens first; 160 left no room for the SMS


def _insert_messages(sid, rec_id, nmis, body, status, only_if_proposed=False):
    """One statement for every message (row-by-row inserts cost a round trip each). A background draft is only written while the
    notice is still proposed, so it can never land beside messages a dispatcher already sent."""
    rows = [dict(m=f"MSG-{uuid.uuid4().hex[:12]}", n=n["nmi"], a="life_support" if n["life_support"] else "customer") for n in nmis[:5000]]
    guard = "WHERE EXISTS (SELECT 1 FROM atlas.recommendation WHERE rec_id = %s AND status = 'proposed')" if only_if_proposed else ""
    pg.q(f"""INSERT INTO atlas.notification_outbox (msg_id, situation_id, rec_id, nmi, audience, channel, body, status)
             SELECT x ->> 'm', %s, %s, x ->> 'n', x ->> 'a', 'sms', %s, %s FROM jsonb_array_elements(%s::jsonb) x {guard}""",
         (sid, rec_id, body, status, json.dumps(rows), *((rec_id,) if only_if_proposed else ())))
    return len(rows), sum(1 for r in rows if r["a"] == "life_support")


_PREPARING = set()


def _prepare_notice(s, r):
    """Done in the background as soon as a notice is proposed: look up who receives it (synced gold) and draft the SMS (LLM), then
    park the messages in the Lakebase outbox as drafts. Accept then only flips their status, so it is instant."""
    try:
        p = r["payload"] or {}
        nmis = _recipients(p.get("audience", "affected"), p.get("feeders") or s["feeders"], p.get("device_id"))
        if nmis:
            _insert_messages(s["situation_id"], r["rec_id"], nmis, _draft_sms(s, p.get("audience", "affected")), "draft", only_if_proposed=True)
    finally:
        _PREPARING.discard(r["rec_id"])


def _prepare_pending(s):
    """Start preparing every proposed notice that has no drafts yet (newly planned, or planned before drafts existed)."""
    for r in s["recommendations"]:
        if r["kind"] == "notify" and r["status"] == "proposed" and r["rec_id"] not in _PREPARING and not pg.q(
                "SELECT 1 FROM atlas.notification_outbox WHERE rec_id = %s AND status = 'draft' LIMIT 1", (r["rec_id"],)):
            _PREPARING.add(r["rec_id"])
            threading.Thread(target=_prepare_notice, args=(s, r), daemon=True).start()


def queue_notifications(s, r, audience, feeders, text=None):
    """Send a notice: promote its prepared drafts (one Lakebase update), applying the dispatcher's own wording or a narrower audience.
    Without drafts (Accept clicked before preparation finished) it does the full lookup and draft inline."""
    have = {x["audience"] for x in pg.q("SELECT DISTINCT audience FROM atlas.notification_outbox WHERE rec_id = %s AND status = 'draft'", (r["rec_id"],))}
    if have and audience != "life_support" and "customer" not in have:   # widened past the drafted life support list: look up again
        pg.q("DELETE FROM atlas.notification_outbox WHERE rec_id = %s AND status = 'draft'", (r["rec_id"],))
        have = set()
    if have:
        if audience == "life_support":
            pg.q("DELETE FROM atlas.notification_outbox WHERE rec_id = %s AND status = 'draft' AND audience <> 'life_support'", (r["rec_id"],))
        got = pg.q("""UPDATE atlas.notification_outbox SET status = 'queued', body = coalesce(%s, body) WHERE rec_id = %s AND status = 'draft'
                      RETURNING audience""", (text, r["rec_id"]))
        n, ls = len(got), sum(1 for g in got if g["audience"] == "life_support")
    else:
        nmis = _recipients(audience, feeders, (r.get("payload") or {}).get("device_id"))
        if not nmis:
            return "No customers to notify."
        n, ls = _insert_messages(s["situation_id"], r["rec_id"], nmis, text or _draft_sms(s, audience), "queued")
    return f"{_qty(n, 'SMS', 'SMS')} queued, {ls:,} of them to life support customers."
